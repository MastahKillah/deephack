// Minimal Chrome DevTools Protocol client for the Electron main process (needs Node 22+ for global WebSocket).
// usage: node cdp.js inject <hookPath>     -- attach to a process started with --inspect-brk, load hook, resume
//        node cdp.js eval "<js expression>" -- evaluate in the main process (has `require` via process.mainModule)
//        node cdp.js evalfile <file.js>     -- __SN__ is replaced by $MYSTIQUE_SN (device serial), __ROOT__ by the repo path
const fs = require('fs');
const PORT = process.env.CDP_PORT || 9229;

async function target(retries = 60) {
  for (let i = 0; i < retries; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const j = await r.json();
      if (j[0]) return j[0].webSocketDebuggerUrl;
    } catch (_) {}
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('inspector not reachable on port ' + PORT);
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    let id = 0;
    const pending = new Map();
    const listeners = [];
    ws.onopen = () => resolve({
      send(method, params = {}) {
        return new Promise((res, rej) => { pending.set(++id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); });
      },
      on(fn) { listeners.push(fn); },
      close() { ws.close(); },
    });
    ws.onerror = (e) => reject(new Error('ws error ' + (e.message || '')));
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const p = pending.get(m.id); pending.delete(m.id);
        m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
      } else if (m.method) listeners.forEach((fn) => fn(m));
    };
  });
}

const show = (r) => {
  if (r.exceptionDetails) return 'EXCEPTION: ' + (r.exceptionDetails.exception && r.exceptionDetails.exception.description || r.exceptionDetails.text);
  const v = r.result;
  return v.value !== undefined ? (typeof v.value === 'string' ? v.value : JSON.stringify(v.value, null, 1)) : (v.description || v.type);
};

(async () => {
  const [cmd, arg] = process.argv.slice(2);
  const c = await connect(await target());
  if (cmd === 'inject') {
    const hook = arg.replace(/\\/g, '/');
    const paused = new Promise((res) => c.on((m) => { if (m.method === 'Debugger.paused') res(m.params); }));
    await c.send('Runtime.enable');
    await c.send('Debugger.enable');
    await c.send('Runtime.runIfWaitingForDebugger');
    const p = await Promise.race([paused, new Promise((_, rej) => setTimeout(() => rej(new Error('never paused')), 30000))]);
    const top = p.callFrames[0];
    console.log('paused at', top.url, 'line', top.location.lineNumber, 'fn', top.functionName || '(top)');
    const expr = `(() => { const r = (typeof require === 'function') ? require : process.mainModule.require.bind(process.mainModule); r(${JSON.stringify(hook)}); return 'hook loaded, electron ' + process.versions.electron + ' node ' + process.versions.node; })()`;
    const r = await c.send('Debugger.evaluateOnCallFrame', { callFrameId: top.callFrameId, expression: expr, returnByValue: true });
    console.log(show(r));
    await c.send('Debugger.resume');
    await c.send('Debugger.disable');
  } else if (cmd === 'eval' || cmd === 'evalfile') {
    const root = require('path').join(__dirname, '..').replace(/\\/g, '/');
    const body = (cmd === 'eval' ? arg : fs.readFileSync(arg, 'utf8'))
      .replaceAll('__SN__', process.env.MYSTIQUE_SN || '').replaceAll('__ROOT__', root);
    const expr = `(async () => { const require = process.mainModule.require.bind(process.mainModule); ${/\breturn\b/.test(body) ? body : 'return (' + body + ')'} })()`;
    const r = await c.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    console.log(show(r));
  } else {
    console.log('unknown command');
  }
  c.close();
  process.exit(0);
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
