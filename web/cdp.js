// Minimal Chrome DevTools Protocol client over a browser WebSocket.
export class CDP {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.ws = null;
    this.closed = true;
  }

  connect(timeoutMs = 8000) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.url);
      const timer = setTimeout(() => { ws.close(); reject(new Error('CDP connect timeout')); }, timeoutMs);
      ws.onopen = () => { clearTimeout(timer); this.closed = false; resolve(this); };
      ws.onerror = () => { clearTimeout(timer); reject(new Error('CDP connection failed: ' + this.url)); };
      ws.onclose = () => {
        this.closed = true;
        for (const { reject: rej } of this.pending.values()) rej(new Error('CDP connection closed'));
        this.pending.clear();
        this.emit('close', {});
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve: res, reject: rej } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
        } else if (msg.method) {
          this.emit(msg.method, msg.params || {});
        }
      };
      this.ws = ws;
    });
  }

  send(method, params = {}, timeoutMs = 30000) {
    if (this.closed) return Promise.reject(new Error('CDP not connected'));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  on(event, cb) {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set());
    this.listeners.get(event).add(cb);
    return () => this.listeners.get(event)?.delete(cb);
  }

  once(event, timeoutMs) {
    return new Promise((resolve) => {
      const off = this.on(event, (p) => { off(); clearTimeout(t); resolve(p); });
      const t = setTimeout(() => { off(); resolve(null); }, timeoutMs);
    });
  }

  emit(event, params) {
    this.listeners.get(event)?.forEach((cb) => { try { cb(params); } catch (e) { console.error(e); } });
  }

  close() { try { this.ws?.close(); } catch {} }
}
