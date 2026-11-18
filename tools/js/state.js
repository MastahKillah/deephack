const { BrowserWindow } = require('electron');
const SN = '__SN__';
const w = BrowserWindow.getAllWindows().find((x) => /index\.html/.test(x.webContents.getURL()));
const inv = (ch, ...a) => w.webContents.executeJavaScript(`window.ipcRenderer.invoke(${JSON.stringify(ch)}, ...${JSON.stringify(a)})`);
return {
  resources: await inv('sys/get-resources', SN),
  info: await inv('mystique/get-device-info', SN),
};
