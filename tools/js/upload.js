const { BrowserWindow } = require('electron');
const SN = '__SN__';
const w = BrowserWindow.getAllWindows().find((x) => /index\.html/.test(x.webContents.getURL()));
const inv = (ch, ...a) => w.webContents.executeJavaScript(`window.ipcRenderer.invoke(${JSON.stringify(ch)}, ...${JSON.stringify(a)})`);
const res = await inv('mystique/upload-image', {
  id: '', path: '__ROOT__/assets/test_480x640.png',
  x: 0, y: 0, width: 480, height: 640, model: 'mystique', rotate: 0, serialNumber: SN,
});
const after = await inv('sys/get-resources', SN);
return { res, jpg: after.data.jpg.map((j) => ({ id: j.id, cropped: j.cropped })), screen: after.data.motionModeScreen };
