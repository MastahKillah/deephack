// Upload a test GIF through the official app (to capture the protocol), then list what the app thinks is stored.
const { BrowserWindow } = require('electron');
const fs = require('fs');
const SN = '__SN__';
const mark = (m) => fs.appendFileSync('__ROOT__/captures/session.jsonl', JSON.stringify({ t: Date.now(), tid: 0, k: 'mark', m }) + '\n');
const w = BrowserWindow.getAllWindows().find((x) => /index\.html/.test(x.webContents.getURL()));
const inv = (ch, ...a) => w.webContents.executeJavaScript(`window.ipcRenderer.invoke(${JSON.stringify(ch)}, ...${JSON.stringify(a)})`);
mark('gif-upload');
const res = await inv('mystique/upload-image', {
  id: '', path: '__ROOT__/assets/test.gif',
  x: 0, y: 0, width: 480, height: 640, model: 'mystique', rotate: 0, serialNumber: SN,
});
const after = await inv('sys/get-resources', SN);
mark('gif-upload-done');
return { res, gif: after.data.gif.map((g) => ({ id: g.id, metadata: g.metadata })), jpg: after.data.jpg.map((g) => g.id), screen: after.data.motionModeScreen };
