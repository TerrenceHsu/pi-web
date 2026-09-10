// No public message receiver, content scripts, host permissions, or shared storage.
let pending = null;
let clicked = null;

chrome.tabs.onCreated.addListener(tab => {
  if (Number.isInteger(tab.id)) chrome.tabs.update(tab.id, {muted: true}).catch(() => {});
});

chrome.action.onClicked.addListener(tab => {
  const request = pending;
  pending = null;
  if (!request || request.expires < Date.now()) return;
  if (!Number.isInteger(tab.id) || tab.incognito !== chrome.extension.inIncognitoContext ||
      (request.expected !== null && request.expected !== tab.id)) {
    clicked = {token: request.token, error: 'browser_media_capture_failed'};
    return;
  }
  chrome.tabs.update(tab.id, {muted: true}).then(() => {
    clicked = {token: request.token, tabId: tab.id};
  }, () => {
    clicked = {token: request.token, error: 'browser_media_capture_failed'};
  });
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || sender.frameId !== 0 ||
      sender.url !== chrome.runtime.getURL('capture.html') ||
      sender.tab?.incognito !== chrome.extension.inIncognitoContext) return;
  if (message?.kind === 'arm' && typeof message.token === 'string' &&
      message.token.length <= 64 &&
      (message.expected === null || Number.isInteger(message.expected))) {
    pending = {token: message.token, expected: message.expected, expires: Date.now() + 5000};
    clicked = null;
    respond({ok: true});
  } else if (message?.kind === 'take' && typeof message.token === 'string') {
    const result = clicked?.token === message.token ? clicked : null;
    if (result) clicked = null;
    respond(result);
  } else if (message?.kind === 'cancel' && typeof message.token === 'string') {
    if (pending?.token === message.token) pending = null;
    if (clicked?.token === message.token) clicked = null;
    respond({ok: true});
  }
});
