// Injected into the DeepCool Electron main process (and its worker threads) via the inspector.
// Logs every USB transfer + IPC call as JSON lines. Does not alter behaviour.
'use strict';
if (!globalThis.__deephack) {
  globalThis.__deephack = true;
  const fs = require('fs');
  const path = require('path');
  const Module = require('module');
  let threadId = 0;
  let wt = null;
  try { wt = require('worker_threads'); threadId = wt.threadId; } catch (_) {}

  const OUT_DIR = path.join(__dirname, '..', 'captures');
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const LOG = path.join(OUT_DIR, 'session.jsonl');
  const t0 = Date.now();
  const log = (o) => {
    try { fs.appendFileSync(LOG, JSON.stringify({ t: Date.now(), tid: threadId, ...o }) + '\n'); } catch (_) {}
  };
  const hex = (b) => Buffer.from(b.buffer ? Buffer.from(b.buffer, b.byteOffset, b.byteLength) : b).toString('hex');
  const stack = () => {
    const s = new Error().stack.split('\n').slice(3, 14)
      .map((l) => l.trim().replace(/^at /, '').replace(/\s*\(.*\)$/, ''))
      .filter((l) => l && !/^(node:|Endpoint\.|OutEndpoint\.|InEndpoint\.|WebUSBDevice\.|new Promise|Generator\.|__awaiter|step|fulfilled|processTicksAndRejections|evalmachine)/.test(l));
    return s.slice(0, 7).join(' < ');
  };
  const safe = (v, max = 3000) => {
    try {
      const s = JSON.stringify(v, (k, x) => {
        if (x && x.type === 'Buffer' && Array.isArray(x.data)) return `<buf ${x.data.length}: ${Buffer.from(x.data.slice(0, 64)).toString('hex')}>`;
        if (x instanceof Uint8Array) return `<u8 ${x.length}: ${Buffer.from(x.slice(0, 64)).toString('hex')}>`;
        if (typeof x === 'string' && x.length > 600) return x.slice(0, 600) + `...(${x.length})`;
        return x;
      });
      return s && s.length > max ? s.slice(0, max) + '...' : s;
    } catch (e) { return `<unserializable ${e.message}>`; }
  };

  log({ k: 'hook-start', pid: process.pid, type: process.type, argv: process.argv.slice(1) });

  // ---- USB ----
  const patched = new WeakSet();
  const patchUsb = () => {
    for (const key of Object.keys(Module._cache)) {
      const m = Module._cache[key];
      if (!m || !m.exports) continue;
      if (/usb[\\/]dist[\\/]usb[\\/]endpoint\.js$/.test(key)) {
        const P = m.exports.Endpoint && m.exports.Endpoint.prototype;
        if (P && !patched.has(P)) {
          patched.add(P);
          const orig = P.makeTransfer;
          P.makeTransfer = function (timeout, callback) {
            const ep = this.address;
            const isIn = (ep & 0x80) !== 0;
            const st = stack();
            let submitted = null;
            const cb = function (error, buf, actual) {
              if (isIn) log({ k: 'in', ep, n: actual, hex: (!error && submitted) ? hex(submitted.slice(0, actual)) : '', err: error ? String(error.message || error) : undefined, st });
              else if (error) log({ k: 'out-err', ep, err: String(error.message || error) });
              return callback && callback.apply(this, arguments);
            };
            const tr = orig.call(this, timeout, cb);
            const origSubmit = tr.submit;
            const hookedSubmit = function (buffer, cb2) {
              submitted = buffer;
              if (!isIn) log({ k: 'out', ep, n: buffer.length, hex: hex(buffer), to: timeout, st });
              if (typeof cb2 === 'function') {
                const w = function (error, b, actual) {
                  if (isIn) log({ k: 'in', ep, n: actual, hex: !error ? hex(b.slice(0, actual)) : '', err: error ? String(error.message || error) : undefined, st: 'poll' });
                  return cb2.apply(this, arguments);
                };
                return origSubmit.call(this, buffer, w);
              }
              return origSubmit.call(this, buffer);
            };
            try {
              Object.defineProperty(tr, 'submit', { value: hookedSubmit, configurable: true, writable: true });
            } catch (e) {
              if (!globalThis.__deephackWarned) { globalThis.__deephackWarned = true; log({ k: 'submit-hook-failed', e: String(e) }); }
            }
            return tr;
          };
          log({ k: 'patched', what: 'Endpoint.makeTransfer', key });
        }
      }
      if (/usb[\\/]dist[\\/]usb[\\/]index\.js$/.test(key)) {
        const D = m.exports.Device && m.exports.Device.prototype;
        if (D && !patched.has(D)) {
          patched.add(D);
          const oc = D.controlTransfer;
          D.controlTransfer = function (bmRequestType, bRequest, wValue, wIndex, dataOrLen, callback) {
            const out = typeof dataOrLen !== 'number';
            log({ k: 'ctrl', bmRequestType, bRequest, wValue, wIndex, data: out ? hex(dataOrLen) : dataOrLen, st: stack() });
            const cb = function (err, data) {
              log({ k: 'ctrl-done', bRequest, err: err ? String(err.message || err) : undefined, data: data && data.length !== undefined && typeof data !== 'number' ? hex(data) : data });
              return callback && callback.apply(this, arguments);
            };
            return oc.call(this, bmRequestType, bRequest, wValue, wIndex, dataOrLen, cb);
          };
          for (const name of ['open', 'close', 'reset', 'setConfiguration']) {
            const o = D[name];
            if (typeof o !== 'function') continue;
            try {
              Object.defineProperty(D, name, { configurable: true, writable: true, value: function (...a) { log({ k: 'dev', op: name, st: stack() }); return o.apply(this, a); } });
            } catch (_) {}
          }
          log({ k: 'patched', what: 'Device', key });
        }
      }
      if (/usb[\\/]dist[\\/]usb[\\/]interface\.js$/.test(key)) {
        const I = m.exports.Interface && m.exports.Interface.prototype;
        if (I && !patched.has(I)) {
          patched.add(I);
          for (const name of ['claim', 'release', 'setAltSetting']) {
            const o = I[name];
            if (typeof o === 'function') I[name] = function (...a) { log({ k: 'iface', op: name, id: this.id, args: safe(a.filter((x) => typeof x !== 'function')) }); return o.apply(this, a); };
          }
        }
      }
    }
  };

  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    const r = origLoad.apply(this, arguments);
    if (request === 'usb' || /[\\/]usb[\\/]/.test(request) || request === 'node-hid') {
      try { patchUsb(); } catch (e) { log({ k: 'patch-error', e: String(e && e.stack || e) }); }
      if (request === 'node-hid') log({ k: 'load', request });
    }
    return r;
  };
  try { patchUsb(); } catch (_) {}

  // ---- worker threads: preload this hook in every worker ----
  if (wt && wt.isMainThread) {
    const OrigWorker = wt.Worker;
    const self = __filename;
    class HookedWorker extends OrigWorker {
      constructor(filename, opts = {}) {
        log({ k: 'worker', filename: String(filename), eval: !!opts.eval });
        if (!opts.eval && typeof filename === 'string' && /\.c?js$/.test(filename)) {
          const code = `require(${JSON.stringify(self)}); require(${JSON.stringify(filename)});`;
          super(code, { ...opts, eval: true });
        } else {
          super(filename, opts);
        }
      }
    }
    try { wt.Worker = HookedWorker; } catch (e) { log({ k: 'worker-patch-error', e: String(e) }); }
  }

  // ---- IPC (main process only) ----
  if (process.type === 'browser') {
    try {
      const { ipcMain } = require('electron');
      const QUIET = /sensor|hwinfo|get-?system|monitor|cpu|gpu|history|log/i;
      const origHandle = ipcMain.handle.bind(ipcMain);
      ipcMain.handle = (channel, fn) => origHandle(channel, async (ev, ...args) => {
        const id = Math.random().toString(36).slice(2, 8);
        const quiet = QUIET.test(channel);
        if (!quiet) log({ k: 'ipc', id, channel, args: safe(args) });
        try {
          const res = await fn(ev, ...args);
          if (!quiet) log({ k: 'ipc-ret', id, channel, res: safe(res) });
          return res;
        } catch (e) {
          log({ k: 'ipc-err', id, channel, e: String(e && e.stack || e) });
          throw e;
        }
      });
      const origOn = ipcMain.on.bind(ipcMain);
      ipcMain.on = (channel, fn) => origOn(channel, (ev, ...args) => {
        if (!QUIET.test(channel)) log({ k: 'ipc-on', channel, args: safe(args) });
        return fn(ev, ...args);
      });
      log({ k: 'patched', what: 'ipcMain' });
    } catch (e) { log({ k: 'ipc-patch-error', e: String(e) }); }
  }
}
