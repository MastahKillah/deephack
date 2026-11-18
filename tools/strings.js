// Extract printable strings (ASCII + UTF-16LE) from a binary.
// usage: node strings.js <file> [minLen=5] [regexFilter]
const fs = require('fs');
const [file, minArg, filter] = process.argv.slice(2);
const min = Number(minArg) || 5;
const re = filter ? new RegExp(filter, 'i') : null;
const buf = fs.readFileSync(file);
const out = [];
const isP = (c) => c >= 0x20 && c < 0x7f;

let s = '', start = 0;
for (let i = 0; i <= buf.length; i++) {
  const c = buf[i];
  if (i < buf.length && isP(c)) { if (!s) start = i; s += String.fromCharCode(c); }
  else { if (s.length >= min) out.push([start, 'A', s]); s = ''; }
}
s = '';
for (let i = 0; i + 1 < buf.length; i += 2) {
  const c = buf[i], h = buf[i + 1];
  if (h === 0 && isP(c)) { if (!s) start = i; s += String.fromCharCode(c); }
  else { if (s.length >= min) out.push([start, 'W', s]); s = ''; }
}
out.sort((a, b) => a[0] - b[0]);
for (const [off, t, str] of out) {
  if (re && !re.test(str)) continue;
  console.log(`${off.toString(16).padStart(8, '0')} ${t} ${str}`);
}
