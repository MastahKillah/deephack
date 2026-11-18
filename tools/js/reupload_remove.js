const { BrowserWindow } = require('electron');
const fs = require('fs');
const SN = '__SN__';
const TEST_ID = '9f5801c74adf43ad844a0f172ca0ee41';
const mark = (m) => fs.appendFileSync('__ROOT__/captures/session.jsonl', JSON.stringify({ t: Date.now(), tid: 0, k: 'mark', m }) + '\n');
const w = BrowserWindow.getAllWindows().find((x) => /index\.html/.test(x.webContents.getURL()));
const inv = (ch, ...a) => w.webContents.executeJavaScript(`window.ipcRenderer.invoke(${JSON.stringify(ch)}, ...${JSON.stringify(a)})`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = {};
mark('reupload-same-id');
out.reupload = await inv('mystique/upload-image', {
  id: TEST_ID, path: '__ROOT__/assets/test2_480x640.png',
  x: 0, y: 0, width: 480, height: 640, model: 'mystique', rotate: 0, serialNumber: SN,
});
out.afterReupload = (await inv('sys/get-resources', SN)).data.jpg.map((j) => j.id);
await sleep(3000);
mark('remove');
out.remove = await inv('mystique/remove-image', SN, TEST_ID);
const r = await inv('sys/get-resources', SN);
out.afterRemove = { jpg: r.data.jpg.map((j) => j.id), gif: r.data.gif.map((j) => j.id), screen: r.data.motionModeScreen };
mark('done');
return out;
