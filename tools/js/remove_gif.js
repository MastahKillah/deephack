// Remove the most recently added GIF through the official app (cleanup after upload_gif.js).
const { BrowserWindow } = require('electron');
const SN = '__SN__';
const w = BrowserWindow.getAllWindows().find((x) => /index\.html/.test(x.webContents.getURL()));
const inv = (ch, ...a) => w.webContents.executeJavaScript(`window.ipcRenderer.invoke(${JSON.stringify(ch)}, ...${JSON.stringify(a)})`);
const before = (await inv('sys/get-resources', SN)).data.gif;
const last = before[before.length - 1];
const res = await inv('mystique/remove-image', SN, last.id);
const after = await inv('sys/get-resources', SN);
return { removed: last.id, res, gifs: after.data.gif.length, jpgs: after.data.jpg.length, screen: after.data.motionModeScreen };
