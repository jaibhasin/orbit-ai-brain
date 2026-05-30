const OFFSCREEN_DOCUMENT_PATH = "offscreen.html";
const captureConfigsByTab = new Map();
let creatingOffscreenDocument = null;

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message) return false;

  if (message.type === "ORBIT_CAPTURE_CONFIG") {
    const tabId = sender.tab && sender.tab.id;
    if (!tabId) {
      sendResponse({ ok: false, error: "No sender tab for Orbit capture config." });
      return true;
    }

    saveCaptureConfig(tabId, message)
      .then(() => sendResponse({ ok: true, cached: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message.type === "ORBIT_USER_START_CAPTURE") {
    startCaptureAndNotify(sender.tab)
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  return false;
});

chrome.action.onClicked.addListener(async (tab) => {
  await startCaptureAndNotify(tab);
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "start-capture") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  await startCaptureAndNotify(tab);
});

async function startCaptureAndNotify(tab) {
  let result;
  try {
    result = await startCaptureForTab(tab);
  } catch (error) {
    result = { ok: false, error: String(error) };
  }
  await notifyCaptureStatus(tab && tab.id, result);
  return result;
}

async function notifyCaptureStatus(tabId, result) {
  if (!tabId) return;
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: "ORBIT_CAPTURE_STATUS",
      ok: Boolean(result && result.ok),
      error: result && result.error
    });
  } catch (error) {
    console.warn("Could not report Orbit capture status to the Meet tab:", error);
  }
}

async function startCaptureForTab(tab) {
  if (!tab || !tab.id) return { ok: false, error: "No active tab." };
  const config = await getCaptureConfig(tab.id);
  if (!config) {
    console.warn("Orbit capture config missing. Wait for Orbit to join Meet, then retry.");
    return { ok: false, error: "Orbit capture config missing." };
  }

  await ensureOffscreenDocument();

  return new Promise((resolve) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id }, async (streamId) => {
      if (chrome.runtime.lastError || !streamId) {
        console.error("Orbit tabCapture failed:", chrome.runtime.lastError);
        resolve({ ok: false, error: chrome.runtime.lastError && chrome.runtime.lastError.message });
        return;
      }

      try {
        const response = await chrome.runtime.sendMessage({
          type: "ORBIT_OFFSCREEN_START",
          streamId,
          tabId: tab.id,
          sessionId: config.sessionId,
          meetingId: config.meetingId,
          webSocketUrl: config.webSocketUrl,
          audioFormat: config.audioFormat || {
            encoding: "linear16",
            sampleRate: 16000,
            channels: 1
          }
        });
        resolve(response || { ok: true });
      } catch (error) {
        resolve({ ok: false, error: String(error) });
      }
    });
  });
}

async function saveCaptureConfig(tabId, config) {
  captureConfigsByTab.set(tabId, config);
  if (chrome.storage && chrome.storage.session) {
    await chrome.storage.session.set({ [`orbitCaptureConfig:${tabId}`]: config });
  }
}

async function getCaptureConfig(tabId) {
  const cached = captureConfigsByTab.get(tabId);
  if (cached) return cached;
  if (!chrome.storage || !chrome.storage.session) return null;

  const key = `orbitCaptureConfig:${tabId}`;
  const stored = await chrome.storage.session.get(key);
  return stored[key] || null;
}

async function ensureOffscreenDocument() {
  if (await hasOffscreenDocument()) return;

  if (!creatingOffscreenDocument) {
    creatingOffscreenDocument = chrome.offscreen.createDocument({
      url: OFFSCREEN_DOCUMENT_PATH,
      reasons: ["USER_MEDIA"],
      justification: "Capture Google Meet tab audio and stream it to the local Orbit backend."
    }).finally(() => {
      creatingOffscreenDocument = null;
    });
  }
  await creatingOffscreenDocument;
}

async function hasOffscreenDocument() {
  if (!chrome.runtime.getContexts) return false;
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [chrome.runtime.getURL(OFFSCREEN_DOCUMENT_PATH)]
  });
  return contexts.length > 0;
}
