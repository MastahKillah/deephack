// Minimal asar reader: list / extract without external deps.
// usage: node asar.js list <asar> [regexFilter]
//        node asar.js extract <asar> <outDir> <regexFilter>
const fs = require('fs');
const path = require('path');

function readHeader(file) {
  const fd = fs.openSync(file, 'r');
  const sizeBuf = Buffer.alloc(16);
  fs.readSync(fd, sizeBuf, 0, 16, 0);
  const headerSize = sizeBuf.readUInt32LE(4);
  const jsonLen = sizeBuf.readUInt32LE(12);
  const buf = Buffer.alloc(jsonLen);
  fs.readSync(fd, buf, 0, jsonLen, 16);
  return { fd, header: JSON.parse(buf.toString('utf8')), base: 8 + headerSize };
}

function* walk(node, prefix) {
  for (const [name, entry] of Object.entries(node.files || {})) {
    const p = prefix ? prefix + '/' + name : name;
    if (entry.files) yield* walk(entry, p);
    else yield [p, entry];
  }
}

const [cmd, asar, a, b] = process.argv.slice(2);
const { fd, header, base } = readHeader(asar);

if (cmd === 'list') {
  const re = a ? new RegExp(a, 'i') : null;
  for (const [p, e] of walk(header, '')) {
    if (re && !re.test(p)) continue;
    console.log(`${String(e.size ?? 0).padStart(10)} ${e.unpacked ? 'U' : ' '} ${p}`);
  }
} else if (cmd === 'extract') {
  const re = new RegExp(b, 'i');
  let n = 0;
  for (const [p, e] of walk(header, '')) {
    if (!re.test(p) || e.unpacked || e.link) continue;
    const out = path.join(a, p);
    if (!path.resolve(out).startsWith(path.resolve(a))) continue;
    fs.mkdirSync(path.dirname(out), { recursive: true });
    const buf = Buffer.alloc(e.size);
    if (e.size) fs.readSync(fd, buf, 0, e.size, base + Number(e.offset));
    fs.writeFileSync(out, buf);
    n++;
  }
  console.log(`extracted ${n} files`);
} else if (cmd === 'summary') {
  // size per top-level/second-level dir
  const agg = {};
  for (const [p, e] of walk(header, '')) {
    const k = p.split('/').slice(0, Number(a) || 2).join('/');
    agg[k] = (agg[k] || 0) + (e.size || 0);
  }
  for (const [k, v] of Object.entries(agg).sort((x, y) => y[1] - x[1]).slice(0, 80))
    console.log(`${String(v).padStart(12)} ${k}`);
}
